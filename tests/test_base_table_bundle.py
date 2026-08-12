from __future__ import annotations

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


def test_构建基表包只解析每个建表文件一次(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert parsed_sql == [table_sql.rstrip(";")]


@pytest.mark.parametrize(
    ("path_name", "sql"),
    (
        ("注释.sql", "-- CREATE TABLE phantom (id INT);\nSET @x=1;"),
        ("字符串.sql", "SELECT 'CREATE TABLE phantom (id INT)';"),
    ),
)
def test_注释或字符串中的_create_table_不作为基表(path_name: str, sql: str) -> None:
    with pytest.raises(RuntimeError, match="^至少需要一张可解析的基表$"):
        build_base_sql_bundle((BaseSqlFile(Path("虚拟目录", path_name), sql),))


def test_注释和字符串中的_create_table_不混入合法表元数据() -> None:
    bundle = build_base_sql_bundle(
        (
            BaseSqlFile(Path("虚拟目录/real.sql"), "CREATE TABLE real_table (id INT);"),
            BaseSqlFile(
                Path("虚拟目录/注释.sql"),
                "-- CREATE TABLE phantom_comment (id INT);\nSET @x=1;",
            ),
            BaseSqlFile(
                Path("虚拟目录/字符串.sql"),
                "SELECT 'CREATE TABLE phantom_string (id INT)';",
            ),
        )
    )

    assert [table.name for table in bundle.tables] == ["real_table"]


def test_损坏的_create_table_在预校验时报告文件名() -> None:
    files = (
        BaseSqlFile(Path("虚拟目录/t0.sql"), "CREATE TABLE t0 (id INT);"),
        BaseSqlFile(Path("虚拟目录/损坏.sql"), "CREATE TEMPORARY TABLE broken (id INT;"),
        BaseSqlFile(Path("虚拟目录/session.sql"), "SET FOREIGN_KEY_CHECKS=0;"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        build_base_sql_bundle(files)

    assert "损坏.sql" in str(exc_info.value)
    assert "括号不完整" in str(exc_info.value)


@pytest.mark.parametrize(
    ("expand_base_table_columns", "generator_version", "seed", "error_message"),
    (
        (False, "v1", None, "未扩展基表列时，生成器版本和种子必须为空"),
        (False, None, "123", "未扩展基表列时，生成器版本和种子必须为空"),
        (True, None, "123", "扩展基表列时，生成器版本和种子不能为空"),
        (True, "v1", None, "扩展基表列时，生成器版本和种子不能为空"),
    ),
)
def test_基表包拒绝模式与生成器信息不一致(
    expand_base_table_columns: bool,
    generator_version: str | None,
    seed: str | None,
    error_message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{error_message}$"):
        BaseSqlBundle(
            files=(),
            tables=(),
            expand_base_table_columns=expand_base_table_columns,
            generator_version=generator_version,
            seed=seed,
        )


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
