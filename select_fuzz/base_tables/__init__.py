"""基表 SQL 内存包。"""

from .loader import build_base_sql_bundle, load_base_sql_bundle
from .models import BaseSqlBundle
from .registry import (
    CURRENT_BASE_TABLE_GENERATOR_VERSION,
    MAX_BASE_TABLE_SEED,
    available_base_table_generator_versions,
    generate_base_sql_bundle,
    generate_core_base_sql_bundle,
    normalize_base_table_seed,
    serialize_bundle,
)

__all__ = [
    "BaseSqlBundle",
    "CURRENT_BASE_TABLE_GENERATOR_VERSION",
    "MAX_BASE_TABLE_SEED",
    "available_base_table_generator_versions",
    "build_base_sql_bundle",
    "generate_base_sql_bundle",
    "generate_core_base_sql_bundle",
    "load_base_sql_bundle",
    "normalize_base_table_seed",
    "serialize_bundle",
]
