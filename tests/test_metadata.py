from pathlib import Path
import re

from select_fuzz.metadata.base_sql import (
    is_base_table_definition,
    is_base_table_definition_file,
    load_base_sql_files,
)
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import BaseSqlFile, ColumnTypeFamily


def test_按文件名顺序读取基表_sql(tmp_path: Path) -> None:
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (id INT);", encoding="utf-8")
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (id INT);", encoding="utf-8")
    (tmp_path / "说明.txt").write_text("ignore", encoding="utf-8")

    files = load_base_sql_files(tmp_path)

    assert [item.path.name for item in files] == ["001_a.sql", "002_b.sql"]
    assert files[0].sql == "CREATE TABLE a (id INT);"


def test_基表_sql_按数字自然顺序读取(tmp_path: Path) -> None:
    for name in ["t10.sql", "t2.sql", "t1.sql", "zz_seed_fk_data.sql"]:
        (tmp_path / name).write_text(f"SELECT '{name}';", encoding="utf-8")

    files = load_base_sql_files(tmp_path)

    assert [item.path.name for item in files] == ["t1.sql", "t2.sql", "t10.sql", "zz_seed_fk_data.sql"]


def test_自定义同名_seed_建表文件仍作为基表(tmp_path: Path) -> None:
    path = tmp_path / "zz_seed_fk_data.sql"
    path.write_text("CREATE TABLE custom_table (id BIGINT NOT NULL, PRIMARY KEY (id));", encoding="utf-8")

    assert is_base_table_definition_file(path) is True
    assert parse_create_table(path.read_text(encoding="utf-8")).name == "custom_table"


def test_生成器种子脚本不作为基表解析() -> None:
    path = Path("sql_base_tables", "zz_seed_fk_data.sql")

    assert is_base_table_definition_file(path) is False


def test_内存基表定义判断只使用_sql_内容() -> None:
    custom_table = BaseSqlFile(
        path=Path("不存在/zz_seed_fk_data.sql"),
        sql="CREATE TABLE custom_table (id BIGINT NOT NULL, PRIMARY KEY (id));",
    )
    generated_seed = BaseSqlFile(
        path=Path("不存在/t0.sql"),
        sql="CREATE TABLE `_select_fuzz_seed_numbers` (`n` INT); /* t0:rows=10 */",
    )

    assert is_base_table_definition(custom_table) is True
    assert is_base_table_definition(generated_seed) is False


def test_解析_create_temporary_table() -> None:
    table = parse_create_table(
        """
        SET FOREIGN_KEY_CHECKS=0;
        CREATE TEMPORARY TABLE `temp_base` (
          `id` BIGINT NOT NULL,
          `name` VARCHAR(64),
          PRIMARY KEY (`id`)
        );
        """
    )

    assert table.name == "temp_base"
    assert table.is_temporary is True
    assert table.columns["name"].type_family is ColumnTypeFamily.STRING


def test_完整基表目录能解析全部表_列族和分区() -> None:
    base_dir = Path("sql_base_tables")
    tables = []
    for sql_file in load_base_sql_files(base_dir):
        if not is_base_table_definition_file(sql_file.path):
            continue
        try:
            tables.append(parse_create_table(sql_file.sql))
        except ValueError:
            continue

    assert [table.name for table in tables] == [f"t{index}" for index in range(79)]
    assert {table.name for table in tables if table.is_temporary} == {f"t{index}" for index in range(2, 7)}
    column_counts = [len(table.columns) for table in tables]
    assert min(column_counts) == 200
    assert max(column_counts) == 500
    assert all(200 <= count <= 500 for count in column_counts)
    families = {column.type_family for table in tables for column in table.columns.values()}
    assert {
        ColumnTypeFamily.INTEGER,
        ColumnTypeFamily.DATETIME,
        ColumnTypeFamily.STRING,
        ColumnTypeFamily.BOOLEAN,
        ColumnTypeFamily.DECIMAL,
        ColumnTypeFamily.FLOAT,
        ColumnTypeFamily.BINARY,
        ColumnTypeFamily.ENUM,
        ColumnTypeFamily.SET,
        ColumnTypeFamily.BIT,
        ColumnTypeFamily.JSON,
    }.issubset(families)
    assert "VECTOR" not in ColumnTypeFamily.__members__
    assert {table.name for table in tables if table.partition is not None} == {f"t{index}" for index in range(7, 79)}
    assert {table.name for table in tables if table.partition and table.partition.subpartition_type} == {
        f"t{index}" for index in range(15, 79)
    }


def test_完整基表_不生成全文索引() -> None:
    for sql_file in load_base_sql_files(Path("sql_base_tables")):
        assert "FULLTEXT KEY" not in sql_file.sql
        assert "FULLTEXT INDEX" not in sql_file.sql


def test_完整基表_临时表不定义外键_普通永久表保留外键() -> None:
    temp_sql = "\n".join(Path("sql_base_tables", f"t{index}.sql").read_text(encoding="utf-8") for index in range(2, 7))
    permanent_sql = "\n".join(Path("sql_base_tables", f"t{index}.sql").read_text(encoding="utf-8") for index in [1])

    assert "CREATE TEMPORARY TABLE" in temp_sql
    assert "FOREIGN KEY" not in temp_sql
    assert "FULLTEXT KEY" not in temp_sql
    assert "FOREIGN KEY" in permanent_sql


def test_完整基表_不生成空间列和空间函数() -> None:
    all_sql = "\n".join(path.read_text(encoding="utf-8") for path in Path("sql_base_tables").glob("*.sql"))

    assert "`point_col`" not in all_sql
    assert "SPATIAL KEY" not in all_sql
    assert "ST_GeomFromText" not in all_sql


def test_完整基表_分区表不定义全文索引() -> None:
    partitioned_sql = "\n".join(
        Path("sql_base_tables", f"t{index}.sql").read_text(encoding="utf-8")
        for index in range(7, 79)
    )

    assert "FULLTEXT KEY" not in partitioned_sql


def test_完整基表_分区表不定义外键_普通永久表保留外键覆盖() -> None:
    non_partitioned_sql = "\n".join(
        Path("sql_base_tables", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [0, 1]
    )
    partitioned_sql = "\n".join(
        Path("sql_base_tables", f"t{index}.sql").read_text(encoding="utf-8")
        for index in range(7, 79)
    )

    assert "FOREIGN KEY" in non_partitioned_sql
    assert "FOREIGN KEY" not in partitioned_sql


def test_完整基表_种子数据不包含向量并覆盖每张表() -> None:
    seed_sql = Path("sql_base_tables", "zz_seed_fk_data.sql").read_text(encoding="utf-8")

    for index in range(79):
        assert f"INSERT INTO `t{index}` " in seed_sql
    assert "VEC_FROMTEXT" not in seed_sql
    assert "STRING_TO_VECTOR" not in seed_sql


def test_解析普通表列索引外键和分区() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS normal_child (
      id BIGINT UNSIGNED NOT NULL,
      parent_id BIGINT UNSIGNED NOT NULL,
      score DECIMAL(10,2) NULL,
      payload JSON NULL,
      created_at DATETIME(6) NOT NULL,
      PRIMARY KEY (id),
      KEY idx_parent (parent_id),
      CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent_table(id)
    ) ENGINE=InnoDB
    PARTITION BY RANGE (id)
    SUBPARTITION BY HASH (parent_id)
    SUBPARTITIONS 2 (
      PARTITION p0 VALUES LESS THAN (1000),
      PARTITION pmax VALUES LESS THAN MAXVALUE
    );
    """

    table = parse_create_table(sql)

    assert table.name == "normal_child"
    assert table.columns["id"].type_family is ColumnTypeFamily.INTEGER
    assert table.columns["score"].type_family is ColumnTypeFamily.DECIMAL
    assert table.columns["payload"].type_family is ColumnTypeFamily.JSON
    assert table.indexes["idx_parent"].columns == ["parent_id"]
    assert table.foreign_keys[0].name == "fk_child_parent"
    assert table.foreign_keys[0].parent_table == "parent_table"
    assert table.partition is not None
    assert table.partition.partition_type == "RANGE"
    assert table.partition.subpartition_type == "HASH"


def test_解析失败时返回清晰中文错误() -> None:
    try:
        parse_create_table("SELECT 1")
    except ValueError as exc:
        assert "只支持解析 CREATE TABLE" in str(exc)
    else:
        raise AssertionError("非 CREATE TABLE SQL 必须失败")
