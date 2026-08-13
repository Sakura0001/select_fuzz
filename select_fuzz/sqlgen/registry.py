"""查询与 CRUD 生成器的永久版本注册和运行时派发入口。"""

from __future__ import annotations

from typing import Callable, Optional

from .dml import DMLGenerator
from .generator import SQLGenerator
from .seeds import CURRENT_CRUD_GENERATOR_VERSION, CURRENT_QUERY_GENERATOR_VERSION


_QueryGeneratorFactory = Callable[[Optional[int], int], SQLGenerator]
_CrudGeneratorFactory = Callable[[Optional[int], str], DMLGenerator]


def _create_query_v1(seed: int | None, max_sql_length: int) -> SQLGenerator:
    return SQLGenerator(random_seed=seed, max_sql_length=max_sql_length)


def _create_crud_v1(seed: int | None, base_table_seed: str) -> DMLGenerator:
    return DMLGenerator(random_seed=seed, base_table_seed=base_table_seed)


# v1 是已发布的复现协议，后续算法变化只能新增版本，不能替换或删除此登记。
_QUERY_GENERATORS: dict[str, _QueryGeneratorFactory] = {"v1": _create_query_v1}
_CRUD_GENERATORS: dict[str, _CrudGeneratorFactory] = {"v1": _create_crud_v1}


def available_query_generator_versions() -> tuple[str, ...]:
    """按登记顺序返回所有仍受支持的查询生成器版本。"""

    return tuple(_QUERY_GENERATORS)


def available_crud_generator_versions() -> tuple[str, ...]:
    """按登记顺序返回所有仍受支持的 CRUD 生成器版本。"""

    return tuple(_CRUD_GENERATORS)


def create_query_generator(
    version: str,
    seed: int | None,
    *,
    max_sql_length: int = 8000,
) -> SQLGenerator:
    """按请求版本创建查询生成器。"""

    try:
        factory = _QUERY_GENERATORS[version]
    except KeyError as exc:
        raise ValueError(f"未知查询生成器版本：{version}") from exc
    generator = factory(seed, max_sql_length)
    generator.generator_version = version
    return generator


def create_crud_generator(
    version: str,
    seed: int | None,
    *,
    base_table_seed: str = "0",
) -> DMLGenerator:
    """按请求版本创建 CRUD 生成器。"""

    try:
        factory = _CRUD_GENERATORS[version]
    except KeyError as exc:
        raise ValueError(f"未知 CRUD 生成器版本：{version}") from exc
    generator = factory(seed, base_table_seed)
    generator.generator_version = version
    return generator


if CURRENT_QUERY_GENERATOR_VERSION not in _QUERY_GENERATORS:
    raise RuntimeError("当前查询生成器版本未登记")
if CURRENT_CRUD_GENERATOR_VERSION not in _CRUD_GENERATORS:
    raise RuntimeError("当前 CRUD 生成器版本未登记")


__all__ = [
    "available_crud_generator_versions",
    "available_query_generator_versions",
    "create_crud_generator",
    "create_query_generator",
]
