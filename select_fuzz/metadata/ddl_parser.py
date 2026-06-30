from __future__ import annotations

import re
from typing import Dict, List, Optional

from .models import (
    ColumnMetadata,
    ColumnTypeFamily,
    ForeignKeyMetadata,
    IndexMetadata,
    PartitionMetadata,
    TableMetadata,
)


_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(?P<name>[\w$]+)`?\s*\(",
    re.IGNORECASE,
)


def parse_create_table(sql: str) -> TableMetadata:
    match = _CREATE_TABLE_RE.search(sql)
    if not match:
        raise ValueError("只支持解析 CREATE TABLE 语句")

    table_name = match.group("name")
    body_start = match.end() - 1
    body_end = _find_matching_paren(sql, body_start)
    body = sql[body_start + 1 : body_end]
    tail = sql[body_end + 1 :]

    columns: Dict[str, ColumnMetadata] = {}
    indexes: Dict[str, IndexMetadata] = {}
    foreign_keys: List[ForeignKeyMetadata] = []

    for item in _split_top_level(body):
        normalized = item.strip()
        if not normalized:
            continue
        upper = normalized.upper()
        if "FOREIGN KEY" in upper:
            foreign_key = _parse_foreign_key(normalized)
            if foreign_key is not None:
                foreign_keys.append(foreign_key)
            continue
        if _is_index_definition(upper):
            index = _parse_index(normalized)
            indexes[index.name] = index
            continue
        column = _parse_column(normalized)
        columns[column.name] = column

    return TableMetadata(
        name=table_name,
        columns=columns,
        indexes=indexes,
        foreign_keys=foreign_keys,
        partition=_parse_partition(tail),
        is_temporary=bool(re.search(r"\bCREATE\s+TEMPORARY\s+TABLE\b", sql, re.IGNORECASE)),
    )


def _find_matching_paren(text: str, start: int) -> int:
    depth = 0
    quote: Optional[str] = None
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("CREATE TABLE 括号不完整")


def _split_top_level(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    quote: Optional[str] = None
    start = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def _parse_column(definition: str) -> ColumnMetadata:
    pieces = definition.split()
    if len(pieces) < 2:
        raise ValueError(f"无法解析列定义: {definition}")
    name = pieces[0].strip("`")
    sql_type = _extract_sql_type(definition)
    upper = definition.upper()
    return ColumnMetadata(
        name=name,
        sql_type=sql_type,
        type_family=_type_family(sql_type),
        nullable="NOT NULL" not in upper,
        invisible="INVISIBLE" in upper,
        generated="GENERATED" in upper or " AS " in upper,
    )


def _extract_sql_type(definition: str) -> str:
    rest = definition.split(None, 1)[1].strip()
    stop_words = [
        " NOT ",
        " NULL",
        " DEFAULT ",
        " COMMENT ",
        " COLLATE ",
        " CHARACTER ",
        " AUTO_INCREMENT",
        " GENERATED ",
        " INVISIBLE",
        " PRIMARY ",
        " UNIQUE ",
    ]
    upper = f" {rest.upper()}"
    end = len(rest)
    for stop in stop_words:
        pos = upper.find(stop)
        if pos > 0:
            end = min(end, pos - 1)
    return rest[:end].strip()


def _type_family(sql_type: str) -> ColumnTypeFamily:
    upper = sql_type.upper()
    if upper.startswith(("TINYINT(1)", "BOOLEAN", "BOOL")):
        return ColumnTypeFamily.BOOLEAN
    if upper.startswith(("TINYINT", "SMALLINT", "MEDIUMINT", "INT", "INTEGER", "BIGINT")):
        return ColumnTypeFamily.INTEGER
    if upper.startswith(("FLOAT", "DOUBLE", "REAL")):
        return ColumnTypeFamily.FLOAT
    if upper.startswith(("DECIMAL", "NUMERIC")):
        return ColumnTypeFamily.DECIMAL
    if upper.startswith(("DATE", "TIME", "YEAR", "DATETIME", "TIMESTAMP")):
        return ColumnTypeFamily.DATETIME
    if upper.startswith(("CHAR", "VARCHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT")):
        return ColumnTypeFamily.STRING
    if upper.startswith(("BINARY", "VARBINARY", "BLOB", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB")):
        return ColumnTypeFamily.BINARY
    if upper.startswith("ENUM"):
        return ColumnTypeFamily.ENUM
    if upper.startswith("SET"):
        return ColumnTypeFamily.SET
    if upper.startswith("BIT"):
        return ColumnTypeFamily.BIT
    if upper.startswith("JSON"):
        return ColumnTypeFamily.JSON
    if upper.startswith(("POINT", "LINESTRING", "POLYGON", "GEOMETRY", "MULTI")):
        return ColumnTypeFamily.SPATIAL
    return ColumnTypeFamily.UNKNOWN


def _is_index_definition(upper: str) -> bool:
    return upper.startswith(
        (
            "PRIMARY KEY",
            "KEY ",
            "INDEX ",
            "UNIQUE KEY",
            "UNIQUE INDEX",
            "FULLTEXT KEY",
            "FULLTEXT INDEX",
            "SPATIAL KEY",
            "SPATIAL INDEX",
        )
    )


def _parse_index(definition: str) -> IndexMetadata:
    upper = definition.upper()
    primary = upper.startswith("PRIMARY KEY")
    fulltext = upper.startswith("FULLTEXT")
    spatial = upper.startswith("SPATIAL")
    unique = upper.startswith("UNIQUE")
    if primary:
        name = "PRIMARY"
    else:
        name_match = re.search(r"(?:KEY|INDEX)\s+`?([\w$]+)`?", definition, re.IGNORECASE)
        name = name_match.group(1) if name_match else "idx_unknown"
    column_match = re.search(r"\((?P<cols>.*?)\)", definition, re.DOTALL)
    columns = _parse_column_list(column_match.group("cols")) if column_match else []
    return IndexMetadata(
        name=name,
        columns=columns,
        unique=unique,
        primary=primary,
        fulltext=fulltext,
        spatial=spatial,
    )


def _parse_column_list(text: str) -> List[str]:
    columns: List[str] = []
    for raw in _split_top_level(text):
        cleaned = raw.strip().split()[0].strip("`")
        columns.append(cleaned)
    return columns


def _parse_foreign_key(definition: str) -> Optional[ForeignKeyMetadata]:
    pattern = re.compile(
        r"(?:CONSTRAINT\s+`?(?P<name>[\w$]+)`?\s+)?FOREIGN\s+KEY\s*\((?P<child>.*?)\)\s+REFERENCES\s+`?(?P<parent>[\w$]+)`?\s*\((?P<parent_cols>.*?)\)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(definition)
    if not match:
        return None
    return ForeignKeyMetadata(
        name=match.group("name") or "fk_unknown",
        child_columns=_parse_column_list(match.group("child")),
        parent_table=match.group("parent"),
        parent_columns=_parse_column_list(match.group("parent_cols")),
    )


def _parse_partition(tail: str) -> Optional[PartitionMetadata]:
    partition_match = re.search(
        r"PARTITION\s+BY\s+(?P<type>(?:LINEAR\s+)?(?:RANGE|LIST|HASH|KEY)(?:\s+COLUMNS)?)\s*\((?P<expr>.*?)\)",
        tail,
        re.IGNORECASE | re.DOTALL,
    )
    if not partition_match:
        return None
    subpartition_match = re.search(
        r"SUBPARTITION\s+BY\s+(?P<type>(?:LINEAR\s+)?(?:RANGE|LIST|HASH|KEY)(?:\s+COLUMNS)?)\s*\((?P<expr>.*?)\)",
        tail,
        re.IGNORECASE | re.DOTALL,
    )
    return PartitionMetadata(
        partition_type=" ".join(partition_match.group("type").upper().split()),
        partition_expr=partition_match.group("expr").strip(),
        subpartition_type=" ".join(subpartition_match.group("type").upper().split()) if subpartition_match else None,
        subpartition_expr=subpartition_match.group("expr").strip() if subpartition_match else None,
    )
