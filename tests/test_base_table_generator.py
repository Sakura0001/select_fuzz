from pathlib import Path

from select_fuzz.metadata.ddl_parser import parse_create_table
from tools import generate_sql_base_tables as generator
from tools import validate_sql_base_tables as validator


PARTITION_TYPES = {
    "RANGE",
    "RANGE COLUMNS",
    "LIST",
    "LIST COLUMNS",
    "HASH",
    "LINEAR HASH",
    "KEY",
    "LINEAR KEY",
}


def _partition_type(sql: str) -> str:
    for line in sql.splitlines():
        if line.startswith("PARTITION BY "):
            return line.removeprefix("PARTITION BY ").split(" (", 1)[0].split(" PARTITIONS ", 1)[0].strip()
    raise AssertionError("缺少 PARTITION BY")


def _subpartition_type(sql: str) -> str:
    for line in sql.splitlines():
        if line.startswith("SUBPARTITION BY "):
            return line.removeprefix("SUBPARTITION BY ").split(" (", 1)[0].split(" SUBPARTITIONS ", 1)[0].strip()
    raise AssertionError("缺少 SUBPARTITION BY")


def test_种子数据每张表生成可复现十到一百行() -> None:
    first_counts = [generator.seed_row_count(index) for index in range(79)]
    second_counts = [generator.seed_row_count(index) for index in range(79)]

    assert first_counts == second_counts
    assert all(10 <= count <= 100 for count in first_counts)
    assert len(set(first_counts)) > 1

    seed_sql = generator.seed_sql()
    for index, count in enumerate(first_counts):
        assert f"/* t{index}:rows={count} */" in seed_sql
        assert f"WHERE `n` <= {count}" in seed_sql
    assert seed_sql.upper().count("INSERT INTO") == 80


def test_基表列类型长度按表可复现随机覆盖范围() -> None:
    profiles = [generator.table_column_profile(index) for index in range(79)]
    assert [generator.table_column_profile(index) for index in range(79)] == profiles
    column_counts = [generator.table_column_count(index) for index in range(79)]
    assert [generator.table_column_count(index) for index in range(79)] == column_counts
    assert min(column_counts) == 200
    assert max(column_counts) == 500
    assert len(set(column_counts)) >= 20

    char_lengths = {profile.char_length for profile in profiles}
    varchar_lengths = {profile.varchar_length for profile in profiles}
    assert min(char_lengths) == 1
    assert max(char_lengths) == 255
    assert min(varchar_lengths) == 1
    assert max(varchar_lengths) == 255

    short_varchar_sql = generator.create_table_sql(1)
    assert "`varchar_col` varchar(1)" in short_varchar_sql
    assert "`idx_t1_varchar_prefix` (`varchar_col`(1))" in short_varchar_sql


def test_基表生成二百到五百列并纳入种子插入() -> None:
    min_sql = generator.create_table_sql(0)
    max_sql = generator.create_table_sql(1)
    min_table = parse_create_table(min_sql)
    max_table = parse_create_table(max_sql)

    assert len(min_table.columns) == 200
    assert len(max_table.columns) == 500
    assert "extra_t0_000" in min_table.columns
    assert "extra_t1_000" in max_table.columns

    seed_sql = generator.seed_sql()
    assert "`extra_t0_000`" in seed_sql
    assert "`extra_t1_000`" in seed_sql


def test_默认生成八种一级分区和六十四种二级分区组合(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)

    table_files = sorted(tmp_path.glob("t*.sql"), key=lambda path: int(path.stem[1:]))

    assert [path.name for path in table_files] == [f"t{index}.sql" for index in range(79)]

    first_level_types = {
        _partition_type((tmp_path / f"t{index}.sql").read_text(encoding="utf-8"))
        for index in range(7, 15)
    }
    subpartition_pairs = {
        (
            _partition_type((tmp_path / f"t{index}.sql").read_text(encoding="utf-8")),
            _subpartition_type((tmp_path / f"t{index}.sql").read_text(encoding="utf-8")),
        )
        for index in range(15, 79)
    }

    assert first_level_types == PARTITION_TYPES
    assert subpartition_pairs == {(outer, inner) for outer in PARTITION_TYPES for inner in PARTITION_TYPES}

    range_range_sql = (tmp_path / "t15.sql").read_text(encoding="utf-8")
    list_list_sql = (tmp_path / "t33.sql").read_text(encoding="utf-8")
    assert "SUBPARTITION p0sp0 VALUES LESS THAN (2)" in range_range_sql
    assert "SUBPARTITION p0sp7 VALUES LESS THAN (MAXVALUE)" in range_range_sql
    assert "SUBPARTITION p0sp0 VALUES IN (1)" in list_list_sql
    assert "SUBPARTITION p0sp7 VALUES IN (8)" in list_list_sql


def test_默认输出不包含向量并可关闭二级分区() -> None:
    normal_sql = generator.create_table_sql(0, include_subpartition=False)
    subpartition_sql = generator.create_table_sql(15, include_subpartition=False)
    seed_sql = generator.seed_sql()

    assert "VECTOR(" not in normal_sql.upper()
    assert "imci_vector_index=" not in normal_sql
    assert "VEC_FROMTEXT(" not in seed_sql
    assert "SUBPARTITION BY" not in subpartition_sql.upper()
    assert "PARTITION BY" in subpartition_sql.upper()


def test_唯一索引转换遵守分区键限制() -> None:
    normal_sql = generator.create_table_sql(0)
    partition_sql = generator.create_table_sql(7)
    subpartition_sql = generator.create_table_sql(15)

    assert "UNIQUE KEY `idx_t0_int_col`" in normal_sql
    assert "KEY `idx_t0_varchar_prefix`" in normal_sql
    assert "UNIQUE KEY `idx_t7_extra_tenant_int`" in partition_sql
    assert "UNIQUE KEY `idx_t7_int_col`" not in partition_sql
    assert "UNIQUE KEY `idx_t11_extra_tenant_int`" not in subpartition_sql


def test_校验器发现分区种子覆盖退化(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)
    seed_path = tmp_path / "zz_seed_fk_data.sql"
    seed_sql = seed_path.read_text(encoding="utf-8")
    seed_path.write_text(seed_sql.replace(validator.TENANT_COVERAGE_EXPR, "1", 1), encoding="utf-8")

    assert validator.main(sql_dir=tmp_path) == 1


def test_校验器发现子分区种子覆盖退化(tmp_path: Path) -> None:
    generator.generate_files(tmp_path)
    seed_path = tmp_path / "zz_seed_fk_data.sql"
    seed_sql = seed_path.read_text(encoding="utf-8")
    seed_path.write_text(seed_sql.replace(validator.SUBPARTITION_COVERAGE_EXPR, "1", 1), encoding="utf-8")

    assert validator.main(sql_dir=tmp_path) == 1
