from pathlib import Path

from tools import generate_sql_base_tables as generator
from tools import validate_sql_base_tables as validator


def test_种子数据每张表生成可复现一到两千行() -> None:
    first_counts = [generator.seed_row_count(index) for index in range(27)]
    second_counts = [generator.seed_row_count(index) for index in range(27)]

    assert first_counts == second_counts
    assert all(1000 <= count <= 2000 for count in first_counts)
    assert len(set(first_counts)) > 1

    seed_sql = generator.seed_sql()
    for index, count in enumerate(first_counts):
        assert f"/* t{index}:rows={count} */" in seed_sql
        assert f"WHERE `n` <= {count}" in seed_sql
    assert seed_sql.upper().count("INSERT INTO") == 28


def test_默认输出不包含向量并可关闭二级分区() -> None:
    normal_sql = generator.create_table_sql(0, include_subpartition=False)
    subpartition_sql = generator.create_table_sql(11, include_subpartition=False)
    seed_sql = generator.seed_sql()

    assert "VECTOR(" not in normal_sql.upper()
    assert "imci_vector_index=" not in normal_sql
    assert "VEC_FROMTEXT(" not in seed_sql
    assert "SUBPARTITION BY" not in subpartition_sql.upper()
    assert "PARTITION BY" in subpartition_sql.upper()


def test_唯一索引转换遵守分区键限制() -> None:
    normal_sql = generator.create_table_sql(0)
    partition_sql = generator.create_table_sql(7)
    subpartition_sql = generator.create_table_sql(11)

    assert "UNIQUE KEY `idx_t0_int_col`" in normal_sql
    assert "UNIQUE KEY `idx_t0_varchar_prefix`" in normal_sql
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
