from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .models import BaseSqlFile


def load_base_sql_files(base_dir: Path | str) -> List[BaseSqlFile]:
    directory = Path(base_dir)
    if not directory.exists():
        raise FileNotFoundError(f"基表 SQL 目录不存在: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"基表 SQL 路径不是目录: {directory}")

    files: List[BaseSqlFile] = []
    for path in sorted(directory.glob("*.sql"), key=_natural_sort_key):
        files.append(BaseSqlFile(path=path, sql=path.read_text(encoding="utf-8").strip()))
    return files


def split_sql_statements(sql: str) -> List[str]:
    statements: List[str] = []
    current: List[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            current.append(char)
            if char == quote and sql[index - 1] != "\\":
                quote = None
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "-" and next_char == "-":
            index = _skip_line_comment(sql, index + 2)
            continue
        if char == "#":
            index = _skip_line_comment(sql, index + 1)
            continue
        if char == "/" and next_char == "*":
            index = _skip_block_comment(sql, index + 2)
            continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1

    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _natural_sort_key(path: Path) -> List[object]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def _skip_line_comment(sql: str, index: int) -> int:
    while index < len(sql) and sql[index] not in "\r\n":
        index += 1
    return index


def _skip_block_comment(sql: str, index: int) -> int:
    end = sql.find("*/", index)
    return len(sql) if end == -1 else end + 2
