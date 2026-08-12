from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from select_fuzz.base_tables import BaseSqlBundle, build_base_sql_bundle, load_base_sql_bundle
from select_fuzz.base_tables import loader
from select_fuzz.metadata.models import BaseSqlFile


def _seed_sql(table_name: str = "t0") -> str:
    return (
        "CREATE TABLE `_select_fuzz_seed_numbers` (`n` INT); "
        f"/* {table_name}:rows=10 */"
    )


def test_内存基表包保持输入顺序并跳过生成器种子脚本() -> None:
    files = (
        BaseSqlFile(Path("虚拟目录/t10.sql"), "CREATE TABLE t10 (id INT);"),
        BaseSqlFile(Path("虚拟目录/zz_seed_fk_data.sql"), _seed_sql("t10")),
        BaseSqlFile(Path("虚拟目录/session.sql"), "SET FOREIGN_KEY_CHECKS=0;"),
        BaseSqlFile(Path("虚拟目录/t2.sql"), "CREATE TABLE t2 (id BIGINT);"),
    )

    bundle = build_base_sql_bundle(
        files,
        expand_base_table_columns=True,
        generator_version="v1",
        seed="123",
    )

    assert isinstance(bundle, BaseSqlBundle)
    assert isinstance(bundle.files, tuple)
    assert isinstance(bundle.tables, tuple)
    assert [item.path.name for item in bundle.files] == [
        "t10.sql",
        "zz_seed_fk_data.sql",
        "session.sql",
        "t2.sql",
    ]
    assert [table.name for table in bundle.tables] == ["t10", "t2"]
    assert bundle.expand_base_table_columns is True
    assert bundle.generator_version == "v1"
    assert bundle.seed == "123"
    with pytest.raises(FrozenInstanceError):
        bundle.seed = "456"  # type: ignore[misc]


def test_构建基表包只解析每个非种子文件一次(monkeypatch: pytest.MonkeyPatch) -> None:
    table_sql = "CREATE TABLE t0 (id INT);"
    ignored_sql = "SET sql_mode = 'STRICT_ALL_TABLES';"
    parsed_sql: list[str] = []
    real_parse_create_table = loader.parse_create_table

    def counting_parse_create_table(sql: str):
        parsed_sql.append(sql)
        return real_parse_create_table(sql)

    monkeypatch.setattr(loader, "parse_create_table", counting_parse_create_table)

    bundle = build_base_sql_bundle(
        (
            BaseSqlFile(Path("不存在/t0.sql"), table_sql),
            BaseSqlFile(Path("不存在/zz_seed_fk_data.sql"), _seed_sql()),
            BaseSqlFile(Path("不存在/session.sql"), ignored_sql),
        )
    )

    assert [table.name for table in bundle.tables] == ["t0"]
    assert parsed_sql == [table_sql, ignored_sql]
    assert [table.name for table in bundle.tables] == ["t0"]
    assert parsed_sql == [table_sql, ignored_sql]


def test_没有可解析表时构建基表包失败() -> None:
    files = (
        BaseSqlFile(Path("不存在/zz_seed_fk_data.sql"), _seed_sql()),
        BaseSqlFile(Path("不存在/session.sql"), "SET FOREIGN_KEY_CHECKS=0;"),
    )

    with pytest.raises(RuntimeError, match="^至少需要一张可解析的基表$"):
        build_base_sql_bundle(files)


def test_目录加载按自然顺序构建基表包(tmp_path: Path) -> None:
    (tmp_path / "t10.sql").write_text("CREATE TABLE t10 (id INT);", encoding="utf-8")
    (tmp_path / "t2.sql").write_text("CREATE TABLE t2 (id INT);", encoding="utf-8")
    (tmp_path / "zz_seed_fk_data.sql").write_text(_seed_sql("t10"), encoding="utf-8")

    bundle = load_base_sql_bundle(tmp_path)

    assert [item.path.name for item in bundle.files] == ["t2.sql", "t10.sql", "zz_seed_fk_data.sql"]
    assert [table.name for table in bundle.tables] == ["t2", "t10"]
