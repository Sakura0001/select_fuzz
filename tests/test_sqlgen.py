import re
from pathlib import Path

from select_fuzz.metadata.base_sql import load_base_sql_files
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.sqlgen.generator import GenerationOptions, SQLGenerator
from select_fuzz.sqlgen.operators import build_operator_registry


def _tables():
    parent = parse_create_table(
        """
        CREATE TABLE parent_table (
          id BIGINT NOT NULL,
          name VARCHAR(64) NOT NULL,
          payload JSON NULL,
          embedding VECTOR(4) NOT NULL,
          PRIMARY KEY (id)
        );
        """
    )
    child = parse_create_table(
        """
        CREATE TABLE child_table (
          child_id BIGINT NOT NULL,
          parent_id BIGINT NOT NULL,
          amount DECIMAL(10,2) NULL,
          created_at DATETIME(6) NOT NULL,
          PRIMARY KEY (child_id),
          KEY idx_parent (parent_id),
          CONSTRAINT fk_child_parent FOREIGN KEY (parent_id) REFERENCES parent_table(id)
        );
        """
    )
    return [parent, child]


def _base_tables():
    tables = []
    for sql_file in load_base_sql_files(Path("sql_base_tables")):
        try:
            tables.append(parse_create_table(sql_file.sql))
        except ValueError:
            continue
    return tables


def test_算子覆盖矩阵包含_select_核心结构和向量算子() -> None:
    registry = build_operator_registry()

    assert registry.has("WITH")
    assert registry.has("WITH RECURSIVE")
    assert registry.has("JOIN ... ON")
    assert registry.has("LEFT JOIN")
    assert registry.has("UNION")
    assert registry.has("WINDOW")
    assert registry.has("FOR UPDATE")
    assert registry.has("CASE WHEN")
    assert registry.has("JSON_ARROW_UNQUOTE")


def test_生成_sql_只引用已知表并包含_cte_join_向量距离() -> None:
    generator = SQLGenerator(random_seed=7, max_sql_length=3000)

    sql = generator.generate(
        _tables(),
        GenerationOptions(require_cte=True, require_join=True, require_vector=True),
    )

    assert "WITH" in sql
    assert "JOIN" in sql
    assert "DISTANCE(" in sql
    assert "parent_table" in sql or "child_table" in sql
    assert "unknown_table" not in sql


def test_生成器记录命中的覆盖项() -> None:
    generator = SQLGenerator(random_seed=11)

    sql = generator.generate(_tables(), GenerationOptions(require_set_operation=True))

    assert "UNION" in sql or "INTERSECT" in sql or "EXCEPT" in sql
    assert generator.coverage_hits
    assert any(hit in generator.coverage_hits for hit in {"UNION", "INTERSECT", "EXCEPT"})


def test_sql_长度保护会回退到简单查询() -> None:
    generator = SQLGenerator(random_seed=3, max_sql_length=40)

    sql = generator.generate(_tables(), GenerationOptions(require_cte=True, require_join=True))

    assert len(sql) <= 40
    assert sql.startswith("SELECT")


def test_默认生成_sql_避免_only_full_group_by_风险() -> None:
    generator = SQLGenerator(random_seed=1)

    sql = generator.generate(_tables())

    assert " GROUP BY " not in sql
    assert " HAVING " not in sql


def test_完整基表生成_sql_只引用已知表列并使用_polarDB_向量函数白名单() -> None:
    tables = _base_tables()
    known_identifiers = {table.name for table in tables}
    known_identifiers.update(column.name for table in tables for column in table.columns.values())
    generator = SQLGenerator(random_seed=101, max_sql_length=6000)

    for _ in range(30):
        sql = generator.generate(tables)
        quoted_identifiers = set(re.findall(r"`([^`]+)`", sql))
        assert quoted_identifiers <= known_identifiers
        upper = sql.upper()
        assert "VEC_DISTANCE" not in upper
        assert "VEC_FROMTEXT" not in upper
        assert "VECTOR_DISTANCE" not in upper
        if "DISTANCE(" in upper:
            assert "STRING_TO_VECTOR(" in upper
            assert any(metric in upper for metric in ["'COSINE'", "'EUCLIDEAN'", "'DOT'"])
        assert "ST_GEOMFROMTEXT" not in upper
        assert "ST_ASTEXT" not in upper


def test_强制生成_select_核心结构() -> None:
    generator = SQLGenerator(random_seed=21, max_sql_length=8000)

    sql = generator.generate(
        _base_tables(),
        GenerationOptions(
            require_cte=True,
            require_join=True,
            require_set_operation=True,
            require_subquery=True,
            require_window=True,
            require_locking=True,
        ),
    )

    upper = sql.upper()
    assert "WITH" in upper
    assert "JOIN" in upper
    assert any(operator in upper for operator in ["UNION", "INTERSECT", "EXCEPT"])
    assert " OVER " in upper
    assert " WINDOW " in upper
    assert any(lock in upper for lock in ["FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE"])
    assert any(subquery in upper for subquery in [" EXISTS ", " IN (SELECT", "(SELECT"])
    assert {"WITH", "JOIN ... ON", "WINDOW"} & generator.coverage_hits


def test_随机递归深度和长度保护稳定() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=88, max_sql_length=2500)

    for _ in range(120):
        sql = generator.generate(tables)
        assert sql.startswith(("SELECT", "WITH", "("))
        assert len(sql) <= 2500


def test_强制生成_polarDB_兼容向量表达式() -> None:
    generator = SQLGenerator(random_seed=31, max_sql_length=8000)

    sql = generator.generate(_base_tables(), GenerationOptions(require_vector=True))
    upper = sql.upper()

    assert "DISTANCE(" in upper
    assert "STRING_TO_VECTOR(" in upper
    assert any(metric in upper for metric in ["'COSINE'", "'EUCLIDEAN'", "'DOT'"])
    assert "VEC_DISTANCE" not in upper
    assert "VEC_FROMTEXT" not in upper
    assert "VECTOR_TO_STRING(" in upper or "DISTANCE(" in upper
