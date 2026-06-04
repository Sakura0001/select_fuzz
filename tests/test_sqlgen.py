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


def test_算子覆盖矩阵包含_select_核心结构和向量算子() -> None:
    registry = build_operator_registry()

    assert registry.has("WITH")
    assert registry.has("JOIN ... ON")
    assert registry.has("UNION")
    assert registry.has("CASE WHEN")
    assert registry.has("DISTANCE_COSINE")
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
