from pathlib import Path
import re

from select_fuzz.metadata.base_sql import load_base_sql_files
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.metadata.models import ColumnTypeFamily


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


def test_解析_create_temporary_table() -> None:
    table = parse_create_table(
        """
        SET FOREIGN_KEY_CHECKS=0;
        CREATE TEMPORARY TABLE `temp_base` (
          `id` BIGINT NOT NULL,
          `embedding` VECTOR(4),
          PRIMARY KEY (`id`)
        );
        """
    )

    assert table.name == "temp_base"
    assert table.is_temporary is True
    assert table.columns["embedding"].type_family is ColumnTypeFamily.VECTOR


def test_no_vector_基表目录能解析全部表和列族() -> None:
    base_dir = Path("sql_base_tables_no_vector_subpartition")
    tables = []
    for sql_file in load_base_sql_files(base_dir):
        try:
            tables.append(parse_create_table(sql_file.sql))
        except ValueError:
            continue

    assert [table.name for table in tables] == [f"t{index}" for index in range(11)]
    assert {table.name for table in tables if table.is_temporary} == {f"t{index}" for index in range(2, 7)}
    assert {table.name: len(table.columns) for table in tables} == {
        **{f"t{index}": 43 for index in range(7)},
        **{f"t{index}": 42 for index in range(7, 11)},
    }
    families = {column.type_family for table in tables for column in table.columns.values()}
    assert ColumnTypeFamily.VECTOR not in families
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
        ColumnTypeFamily.SPATIAL,
    }.issubset(families)


def test_no_vector_全文索引避免混合字符集_text_列() -> None:
    for sql_file in load_base_sql_files(Path("sql_base_tables_no_vector_subpartition")):
        for line in sql_file.sql.splitlines():
            if "FULLTEXT KEY" in line:
                assert "`tinytext_col`" in line
                assert "`text_col`" not in line
                assert "`mediumtext_col`" not in line
                assert "`longtext_col`" not in line


def test_no_vector_临时表不定义外键_永久表保留外键() -> None:
    temp_sql = "\n".join(Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8") for index in range(2, 7))
    permanent_sql = "\n".join(Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8") for index in [1, 7, 8, 9, 10])

    assert "CREATE TEMPORARY TABLE" in temp_sql
    assert "FOREIGN KEY" not in temp_sql
    assert "FULLTEXT KEY" not in temp_sql
    assert "FOREIGN KEY" in permanent_sql
    assert "FULLTEXT KEY" in permanent_sql


def test_no_vector_分区表不包含空间列_非分区表保留空间覆盖() -> None:
    non_partitioned_sql = "\n".join(
        Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [0, 1]
    )
    partitioned_sql = "\n".join(
        Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [7, 8, 9, 10]
    )
    seed_sql = Path("sql_base_tables_no_vector_subpartition", "zz_seed_fk_data.sql").read_text(encoding="utf-8")

    assert "`point_col` point" in non_partitioned_sql
    assert "SPATIAL KEY" in non_partitioned_sql
    assert "`point_col` point" not in partitioned_sql
    assert "SPATIAL KEY" not in partitioned_sql
    for table_name in ["t7", "t8", "t9", "t10"]:
        for line in seed_sql.splitlines():
            if line.startswith(f"INSERT INTO `{table_name}`"):
                assert "`point_col`" not in line
                assert "ST_GeomFromText" not in line


def test_no_vector_分区表不定义全文索引_非分区表保留全文覆盖() -> None:
    non_partitioned_sql = "\n".join(
        Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [0, 1]
    )
    partitioned_sql = "\n".join(
        Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [7, 8, 9, 10]
    )

    assert "FULLTEXT KEY" in non_partitioned_sql
    assert "FULLTEXT KEY" not in partitioned_sql


def test_no_vector_分区表不定义外键_非分区永久表保留外键覆盖() -> None:
    non_partitioned_sql = "\n".join(
        Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [0, 1]
    )
    partitioned_sql = "\n".join(
        Path("sql_base_tables_no_vector_subpartition", f"t{index}.sql").read_text(encoding="utf-8")
        for index in [7, 8, 9, 10]
    )

    assert "FOREIGN KEY" in non_partitioned_sql
    assert "FOREIGN KEY" not in partitioned_sql


def test_no_vector_种子空间点符合_srid_4326_坐标范围() -> None:
    seed_sql = Path("sql_base_tables_no_vector_subpartition", "zz_seed_fk_data.sql").read_text(encoding="utf-8")

    for match in re.finditer(r"ST_GeomFromText\('POINT\(([-0-9.]+) ([-0-9.]+)\)', 4326\)", seed_sql):
        latitude = float(match.group(1))
        longitude = float(match.group(2))
        assert -90 <= latitude <= 90
        assert -180 <= longitude <= 180


def test_解析普通表列索引外键分区和向量列() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS vector_child (
      id BIGINT UNSIGNED NOT NULL,
      parent_id BIGINT UNSIGNED NOT NULL,
      score DECIMAL(10,2) NULL,
      payload JSON NULL,
      embedding VECTOR(4) NOT NULL COMMENT '向量列',
      created_at DATETIME(6) NOT NULL,
      PRIMARY KEY (id),
      KEY idx_parent (parent_id),
      KEY idx_vector (embedding) COMMENT 'imci_vector_index=HNSW(metric=cosine)',
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

    assert table.name == "vector_child"
    assert table.columns["id"].type_family is ColumnTypeFamily.INTEGER
    assert table.columns["score"].type_family is ColumnTypeFamily.DECIMAL
    assert table.columns["payload"].type_family is ColumnTypeFamily.JSON
    assert table.columns["embedding"].type_family is ColumnTypeFamily.VECTOR
    assert table.columns["embedding"].vector_dimensions == 4
    assert table.indexes["idx_parent"].columns == ["parent_id"]
    assert table.indexes["idx_vector"].is_vector is True
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
