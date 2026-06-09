import re
from pathlib import Path

from select_fuzz.metadata.base_sql import is_base_table_definition_file, load_base_sql_files
from select_fuzz.metadata.ddl_parser import parse_create_table
from select_fuzz.sqlgen.generator import GenerationOptions, SQLGenerator, TableRef
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
        if not is_base_table_definition_file(sql_file.path):
            continue
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
    assert not registry.has("INTERSECT")
    assert not registry.has("EXCEPT")
    assert registry.has("WINDOW")
    assert registry.has("FOR UPDATE")
    assert registry.has("CASE WHEN")
    assert registry.has("JSON_ARROW_UNQUOTE")
    assert registry.has("VEC_FROMTEXT")
    assert registry.has("VEC_TOTEXT")
    assert registry.has("VEC_DISTANCE_COSINE")
    assert registry.has("VEC_DISTANCE_EUCLIDEAN")
    assert not registry.has("DISTANCE_DOT")


def test_mysql_8022_扩展覆盖矩阵只登记当前版本支持语法() -> None:
    registry = build_operator_registry()

    for name in [
        "SELECT CONSTANT",
        "SELECT DISTINCTROW",
        "HIGH_PRIORITY",
        "SQL_SMALL_RESULT",
        "SQL_BIG_RESULT",
        "SQL_BUFFER_RESULT",
        "SQL_CALC_FOUND_ROWS",
        "TABLE",
        "PARENTHESIZED_QUERY",
        "EXPLICIT PARTITION",
        "NOT BETWEEN",
        "NOT IN",
        "NOT EXISTS",
        "NOT LIKE",
        "NOT REGEXP",
        "RLIKE",
        "LIKE ESCAPE",
        "IS TRUE",
        "COUNT DISTINCT",
        "BIT_AND",
        "BIT_OR",
        "BIT_XOR",
        "LAG",
        "LEAD",
        "NTILE",
        "FIRST_VALUE",
        "LAST_VALUE",
        "WINDOW FRAME",
        "JSON_TABLE",
        "JSON_CONTAINS",
        "JSON_KEYS",
        "JSON_LENGTH",
        "COLLATE",
        "BINARY",
        "ABS",
        "ROUND",
        "FLOOR",
        "CEILING",
        "CRC32",
        "TIMESTAMPDIFF",
        "DATE_FORMAT",
        "MONTH",
        "DAYOFWEEK",
    ]:
        assert registry.has(name)

    for unsupported in ["INTERSECT", "INTERSECT ALL", "EXCEPT", "EXCEPT ALL", "MINUS", "SQL_CACHE", "SQL_NO_CACHE"]:
        assert not registry.has(unsupported)


def test_mysql_8022_可强制生成新增查询形态() -> None:
    tables = _base_tables()

    constant_sql = SQLGenerator(random_seed=801).generate(tables, GenerationOptions(require_feature="constant_select"))
    assert constant_sql.upper().startswith("SELECT ")
    assert " FROM " not in constant_sql.upper()

    derived_sql = SQLGenerator(random_seed=802).generate(tables, GenerationOptions(require_feature="constant_derived_table"))
    assert "FROM (SELECT 1 AS const_value)" in derived_sql

    values_sql = SQLGenerator(random_seed=803).generate(tables, GenerationOptions(require_feature="values_statement"))
    assert values_sql.upper().startswith("VALUES ROW(")

    table_sql = SQLGenerator(random_seed=804).generate(tables, GenerationOptions(require_feature="table_statement"))
    assert table_sql.upper().startswith("TABLE `T")

    parenthesized_sql = SQLGenerator(random_seed=805).generate(tables, GenerationOptions(require_feature="parenthesized_query"))
    assert parenthesized_sql.startswith("(")
    assert parenthesized_sql.endswith(")")

    partition_sql = SQLGenerator(random_seed=806).generate(tables, GenerationOptions(require_feature="partition_source"))
    assert " PARTITION (" in partition_sql.upper()


def test_mysql_8022_新增谓词和_select_modifier_能被强制覆盖() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=807, max_sql_length=8000)
    sqls = [
        generator.generate(
            tables,
            GenerationOptions(
                require_feature="predicate_extensions",
                invalid_sql_ratio=0.0,
                null_compare_ratio=0.0,
                risky_expr_ratio=0.0,
            ),
        )
        for _ in range(240)
    ]
    combined = "\n".join(sqls).upper()

    assert " NOT IN " in combined
    assert " NOT EXISTS " in combined
    assert " NOT BETWEEN " in combined
    assert " NOT LIKE " in combined
    assert " NOT REGEXP " in combined
    assert " RLIKE " in combined
    assert " ESCAPE " in combined
    assert re.search(r"\bIS\s+(?:NOT\s+)?(?:TRUE|FALSE|UNKNOWN)\b", combined)

    modifier_generator = SQLGenerator(random_seed=808, max_sql_length=8000)
    for _ in range(240):
        modifier_generator.generate(tables, GenerationOptions(require_feature="select_modifiers"))

    for name in [
        "SELECT DISTINCTROW",
        "HIGH_PRIORITY",
        "SQL_SMALL_RESULT",
        "SQL_BIG_RESULT",
        "SQL_BUFFER_RESULT",
        "SQL_CALC_FOUND_ROWS",
    ]:
        assert name in modifier_generator.coverage_counts


def test_mysql_8022_聚合窗口_json_字符集和函数扩展能被强制覆盖() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=809, max_sql_length=8000)
    for feature in [
        "aggregate_extensions",
        "window_extensions",
        "json_table",
        "json_functions",
        "collation_binary",
        "math_datetime_functions",
    ]:
        for _ in range(220):
            generator.generate(
                tables,
                GenerationOptions(
                    require_feature=feature,
                    invalid_sql_ratio=0.0,
                    null_compare_ratio=0.0,
                    risky_expr_ratio=0.0,
                ),
            )

    for name in [
        "COUNT DISTINCT",
        "BIT_AND",
        "BIT_OR",
        "BIT_XOR",
        "GROUP_CONCAT ORDER",
        "LAG",
        "LEAD",
        "NTILE",
        "FIRST_VALUE",
        "LAST_VALUE",
        "WINDOW FRAME",
        "JSON_TABLE",
        "JSON_CONTAINS",
        "JSON_KEYS",
        "JSON_LENGTH",
        "COLLATE",
        "BINARY",
        "ABS",
        "ROUND",
        "FLOOR",
        "CEILING",
        "CRC32",
        "TIMESTAMPDIFF",
        "DATE_FORMAT",
        "MONTH",
        "DAYOFWEEK",
    ]:
        assert name in generator.coverage_counts


def test_mysql_8022_新增扩展不生成当前版本不支持语法() -> None:
    tables = _base_tables()
    unsupported_patterns = [
        r"\bINTERSECT\b",
        r"\bEXCEPT\b",
        r"\bMINUS\b",
        r"\bSQL_CACHE\b",
        r"\bSQL_NO_CACHE\b",
    ]

    for feature in [
        "constant_select",
        "constant_derived_table",
        "values_statement",
        "table_statement",
        "parenthesized_query",
        "partition_source",
        "predicate_extensions",
        "select_modifiers",
        "aggregate_extensions",
        "window_extensions",
        "json_table",
        "json_functions",
        "collation_binary",
        "math_datetime_functions",
    ]:
        generator = SQLGenerator(random_seed=810, max_sql_length=8000)
        for _ in range(80):
            sql = generator.generate(
                tables,
                GenerationOptions(
                    require_feature=feature,
                    invalid_sql_ratio=0.0,
                    null_compare_ratio=0.0,
                    risky_expr_ratio=0.0,
                ),
            )
            upper = sql.upper()
            for pattern in unsupported_patterns:
                assert re.search(pattern, upper) is None, sql


def test_mysql_8022_新增扩展语法会进入默认随机流量() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=811, max_sql_length=8000)

    for _ in range(5000):
        sql = generator.generate(
            tables,
            GenerationOptions(invalid_sql_ratio=0.0, null_compare_ratio=0.0, risky_expr_ratio=0.0),
        )
        upper = sql.upper()
        assert "INTERSECT" not in upper
        assert "EXCEPT" not in upper
        assert "MINUS" not in upper
        assert "SQL_CACHE" not in upper
        assert "SQL_NO_CACHE" not in upper

    for name in [
        "SELECT CONSTANT",
        "TABLE",
        "EXPLICIT PARTITION",
        "NOT IN",
        "NOT LIKE",
        "COUNT DISTINCT",
        "LAG",
        "JSON_TABLE",
        "COLLATE",
        "ABS",
    ]:
        assert name in generator.coverage_counts


def test_生成_sql_只引用已知表并包含_cte_join_向量距离() -> None:
    generator = SQLGenerator(random_seed=7, max_sql_length=3000)

    sql = generator.generate(
        _tables(),
        GenerationOptions(require_cte=True, require_join=True, require_vector=True),
    )

    assert "WITH" in sql
    assert "JOIN" in sql
    assert "VEC_DISTANCE_" in sql
    assert "parent_table" in sql or "child_table" in sql
    assert "unknown_table" not in sql


def test_生成器记录命中的覆盖项() -> None:
    generator = SQLGenerator(random_seed=11)

    sql = generator.generate(_tables(), GenerationOptions(require_set_operation=True))

    assert "UNION" in sql
    assert "INTERSECT" not in sql
    assert "EXCEPT" not in sql
    assert generator.coverage_hits
    assert "UNION" in generator.coverage_hits
    assert "INTERSECT" not in generator.coverage_hits
    assert "EXCEPT" not in generator.coverage_hits


def test_mysql_8022_集合运算只生成_union() -> None:
    tables = _base_tables()

    for seed in range(40):
        generator = SQLGenerator(random_seed=seed, max_sql_length=8000)

        sql = generator.generate(tables, GenerationOptions(require_set_operation=True))
        upper = sql.upper()

        assert "UNION" in upper
        assert "INTERSECT" not in upper
        assert "EXCEPT" not in upper


def test_null_比较会进入普通谓词覆盖() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=202, max_sql_length=8000)
    null_compare_sql = []

    for _ in range(80):
        sql = generator.generate(tables, GenerationOptions(null_compare_ratio=1.0, invalid_sql_ratio=0.0))
        upper = sql.upper()
        if any(pattern in upper for pattern in ["<=> NULL", "NULL <=>", " = NULL", " <> NULL", " != NULL", " BETWEEN NULL", " IN (NULL"]):
            null_compare_sql.append(sql)

    assert null_compare_sql
    assert any("<=> NULL" in sql.upper() or "NULL <=>" in sql.upper() for sql in null_compare_sql)


def test_故意不合法_sql_会标记风险分类() -> None:
    generator = SQLGenerator(random_seed=303, max_sql_length=8000)

    sql = generator.generate(
        _base_tables(),
        GenerationOptions(invalid_sql_ratio=1.0, null_compare_ratio=0.0, risky_expr_ratio=0.0),
    )

    assert sql
    assert generator.last_sql_validity == "故意不合法"
    assert generator.last_expected_error is True
    assert "invalid_function_arity" in generator.last_risk_tags


def test_null_风险比较不会标记为预期错误() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=505, max_sql_length=8000)

    for _ in range(80):
        generator.generate(
            tables,
            GenerationOptions(null_compare_ratio=1.0, invalid_sql_ratio=0.0, risky_expr_ratio=0.0),
        )
        if "null_compare" in generator.last_risk_tags:
            assert generator.last_sql_validity == "风险"
            assert generator.last_expected_error is False
            return

    raise AssertionError("未生成 NULL 风险比较")


def test_风险表达式比例为_1_时不会递归失败() -> None:
    generator = SQLGenerator(random_seed=606, max_sql_length=8000)

    for _ in range(20):
        sql = generator.generate(
            _base_tables(),
            GenerationOptions(invalid_sql_ratio=0.0, null_compare_ratio=0.0, risky_expr_ratio=1.0),
        )
        assert sql


def test_抽中故意不合法_sql_时不会被空_where_吞掉() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=707, max_sql_length=8000)
    generator._active_options = GenerationOptions(invalid_sql_ratio=0.03, null_compare_ratio=0.0)
    generator._reset_attempt()
    generator._attempt_should_generate_invalid = True
    generator.random.random = lambda: 0.0

    where_clause = generator._where_clause([TableRef(table=tables[0], alias="t0")], tables, depth=1, require_subquery=False)

    assert "JSON_EXTRACT(JSON_OBJECT('k', 'v'))" in where_clause
    assert generator._attempt_should_generate_invalid is False


def test_mysql_8022_扩展语法会被随机覆盖() -> None:
    tables = _base_tables()
    generator = SQLGenerator(random_seed=404, max_sql_length=8000)

    for _ in range(1200):
        generator.generate(tables)

    assert "RANK" in generator.coverage_counts
    assert "DENSE_RANK" in generator.coverage_counts
    assert "DERIVED_TABLE" in generator.coverage_counts
    assert "VALUES" in generator.coverage_counts
    assert "MEMBER OF" in generator.coverage_counts
    assert "JSON_ARRAYAGG" in generator.coverage_counts
    assert "IF" in generator.coverage_counts
    assert "~" in generator.coverage_counts


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


def test_完整基表生成_sql_只引用已知表列并使用当前环境向量函数白名单() -> None:
    tables = _base_tables()
    known_identifiers = {table.name for table in tables}
    known_identifiers.update(column.name for table in tables for column in table.columns.values())
    generator = SQLGenerator(random_seed=101, max_sql_length=6000)

    for _ in range(30):
        sql = generator.generate(tables)
        quoted_identifiers = set(re.findall(r"`([^`]+)`", sql))
        assert quoted_identifiers <= known_identifiers
        upper = sql.upper()
        assert "STRING_TO_VECTOR" not in upper
        assert "VECTOR_TO_STRING" not in upper
        assert "DISTANCE(" not in upper
        assert "VEC_DISTANCE_DOT" not in upper
        assert "'DOT'" not in upper
        assert "VECTOR_DISTANCE" not in upper
        if "VEC_DISTANCE_" in upper:
            assert "VEC_FROMTEXT(" in upper
            assert any(function in upper for function in ["VEC_DISTANCE_COSINE(", "VEC_DISTANCE_EUCLIDEAN("])
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
    assert "UNION" in upper
    assert "INTERSECT" not in upper
    assert "EXCEPT" not in upper
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
        assert sql.startswith(("SELECT", "WITH", "(", "TABLE", "VALUES"))
        assert len(sql) <= 2500


def test_强制生成当前环境兼容向量表达式() -> None:
    generator = SQLGenerator(random_seed=31, max_sql_length=8000)

    sql = generator.generate(_base_tables(), GenerationOptions(require_vector=True))
    upper = sql.upper()

    assert "VEC_FROMTEXT(" in upper
    assert any(function in upper for function in ["VEC_DISTANCE_COSINE(", "VEC_DISTANCE_EUCLIDEAN("])
    assert "VEC_TOTEXT(" in upper or "VEC_DISTANCE_" in upper
    assert "STRING_TO_VECTOR(" not in upper
    assert "VECTOR_TO_STRING(" not in upper
    assert "DISTANCE(" not in upper
    assert "VEC_DISTANCE_DOT" not in upper
    assert "'DOT'" not in upper
