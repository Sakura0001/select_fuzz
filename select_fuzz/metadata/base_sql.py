from __future__ import annotations

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
    for path in sorted(directory.glob("*.sql")):
        files.append(BaseSqlFile(path=path, sql=path.read_text(encoding="utf-8").strip()))
    return files
