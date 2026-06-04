from pathlib import Path

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
