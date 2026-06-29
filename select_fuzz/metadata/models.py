from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class ColumnTypeFamily(str, Enum):
    INTEGER = "整数"
    FLOAT = "浮点"
    DECIMAL = "DECIMAL"
    BOOLEAN = "布尔"
    DATETIME = "日期时间"
    STRING = "字符串"
    BINARY = "二进制"
    ENUM = "枚举"
    SET = "集合"
    BIT = "BIT"
    JSON = "JSON"
    SPATIAL = "空间"
    UNKNOWN = "未知"


@dataclass(frozen=True)
class BaseSqlFile:
    path: Path
    sql: str


@dataclass(frozen=True)
class ColumnMetadata:
    name: str
    sql_type: str
    type_family: ColumnTypeFamily
    nullable: bool = True
    invisible: bool = False
    generated: bool = False


@dataclass(frozen=True)
class IndexMetadata:
    name: str
    columns: List[str]
    unique: bool = False
    primary: bool = False
    fulltext: bool = False
    spatial: bool = False


@dataclass(frozen=True)
class ForeignKeyMetadata:
    name: str
    child_columns: List[str]
    parent_table: str
    parent_columns: List[str]


@dataclass(frozen=True)
class PartitionMetadata:
    partition_type: str
    partition_expr: str
    subpartition_type: Optional[str] = None
    subpartition_expr: Optional[str] = None


@dataclass(frozen=True)
class TableMetadata:
    name: str
    columns: Dict[str, ColumnMetadata] = field(default_factory=dict)
    indexes: Dict[str, IndexMetadata] = field(default_factory=dict)
    foreign_keys: List[ForeignKeyMetadata] = field(default_factory=list)
    partition: Optional[PartitionMetadata] = None
    is_temporary: bool = False
