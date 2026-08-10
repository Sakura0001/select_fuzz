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


def test_算子覆盖矩阵包含_select_核心结构且不包含向量算子() -> None:
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
    assert not registry.has("VEC_FROMTEXT")
    assert not registry.has("VEC_TOTEXT")
    assert not registry.has("VEC_DISTANCE_COSINE")
    assert not registry.has("VEC_DISTANCE_EUCLIDEAN")
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
        "USE INDEX",
        "FORCE INDEX",
        "IGNORE INDEX",
        "INDEX HINT FOR JOIN",
        "INDEX HINT FOR ORDER BY",
        "INDEX HINT FOR GROUP BY",
        "OPTIMIZER HINT",
        "JOIN_ORDER",
        "JOIN_FIXED_ORDER",
        "NO_MERGE",
        "SET_VAR",
        "JOIN_INDEX",
        "NO_INDEX",
        "ROW CONSTRUCTOR",
        "ROW IN",
        "ROW COMPARE",
        "ANY SUBQUERY",
        "SOME SUBQUERY",
        "ALL SUBQUERY",
        "CORRELATED SUBQUERY",
        "LATERAL DERIVED_TABLE",
        "ORDER BY FIELD",
        "ORDER BY RAND",
        "ORDER BY POSITION",
        "RAND",
        "USER",
        "CURRENT_USER",
        "DATABASE",
        "VERSION",
        "CONNECTION_ID",
        "HEX_LITERAL",
        "BIT_LITERAL",
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


def test_mysql_8022_索引提示和优化器提示能被强制覆盖() -> None:
    tables = _base_tables()

    index_generator = SQLGenerator(random_seed=812, max_sql_length=8000)
    index_sqls = [
        index_generator.generate(
            tables,
            GenerationOptions(
                require_feature="index_hints",
                invalid_sql_ratio=0.0,
                null_compare_ratio=0.0,
                risky_expr_ratio=0.0,
            ),
        )
        for _ in range(240)
    ]
    index_combined = "\n".join(index_sqls)
    index_upper = index_combined.upper()
    known_index_names = {index.name for table in tables for index in table.indexes.values() if index.columns}

    assert re.search(r"\b(?:USE|FORCE|IGNORE) INDEX(?: FOR (?:JOIN|ORDER BY|GROUP BY))?\s+\(", index_upper)
    assert any(name in index_combined for name in known_index_names)
    for name in [
        "USE INDEX",
        "FORCE INDEX",
        "IGNORE INDEX",
        "INDEX HINT FOR JOIN",
        "INDEX HINT FOR ORDER BY",
        "INDEX HINT FOR GROUP BY",
    ]:
        assert name in index_generator.coverage_counts

    hint_generator = SQLGenerator(random_seed=813, max_sql_length=8000)
    hint_sqls = [
        hint_generator.generate(
            tables,
            GenerationOptions(
                require_feature="optimizer_hints",
                invalid_sql_ratio=0.0,
                null_compare_ratio=0.0,
                risky_expr_ratio=0.0,
            ),
        )
        for _ in range(260)
    ]
    hint_combined = "\n".join(hint_sqls).upper()

    assert "SELECT /*+" in hint_combined
    assert re.search(r"/\*\+[^*]*(?:T0|T1)", hint_combined)
    for sql in hint_sqls:
        if "JOIN_INDEX(" in sql or "NO_INDEX(" in sql:
            hinted_block = sql[sql.upper().index("SELECT /*+") :]
            assert "(SELECT * FROM" not in hinted_block
    for name in ["OPTIMIZER HINT", "JOIN_ORDER", "JOIN_FIXED_ORDER", "NO_MERGE", "SET_VAR", "JOIN_INDEX", "NO_INDEX"]:
        assert name in hint_generator.coverage_counts


def test_mysql_8022_行构造器_量化子查询_相关子查询和_lateral_能被强制覆盖() -> None:
    tables = _base_tables()

    row_generator = SQLGenerator(random_seed=814, max_sql_length=8000)
    row_sqls = [
        row_generator.generate(
            tables,
            GenerationOptions(
                require_feature="row_constructor",
                invalid_sql_ratio=0.0,
                null_compare_ratio=0.0,
                risky_expr_ratio=0.0,
            ),
        )
        for _ in range(160)
    ]
    row_combined = "\n".join(row_sqls)
    assert re.search(r"\([^)]+`\w+`[^)]*,[^)]+`\w+`[^)]*\)\s+(?:IN|=|<=>|<>|>|>=|<|<=)\s+\(", row_combined)
    for name in ["ROW CONSTRUCTOR", "ROW IN", "ROW COMPARE"]:
        assert name in row_generator.coverage_counts

    quantified_generator = SQLGenerator(random_seed=815, max_sql_length=8000)
    for _ in range(220):
        quantified_generator.generate(
            tables,
            GenerationOptions(
                require_feature="quantified_subqueries",
                invalid_sql_ratio=0.0,
                null_compare_ratio=0.0,
                risky_expr_ratio=0.0,
            ),
        )
    for name in ["ANY SUBQUERY", "SOME SUBQUERY", "ALL SUBQUERY"]:
        assert name in quantified_generator.coverage_counts

    correlated_sql = SQLGenerator(random_seed=816, max_sql_length=8000).generate(
        tables,
        GenerationOptions(
            require_feature="correlated_subquery",
            invalid_sql_ratio=0.0,
            null_compare_ratio=0.0,
            risky_expr_ratio=0.0,
        ),
    )
    assert "EXISTS (SELECT 1 FROM" in correlated_sql
    assert re.search(r"EXISTS \(SELECT 1 FROM .* WHERE .*t0\.", correlated_sql)

    lateral_sql = SQLGenerator(random_seed=817, max_sql_length=8000).generate(
        tables,
        GenerationOptions(
            require_feature="lateral_derived_table",
            invalid_sql_ratio=0.0,
            null_compare_ratio=0.0,
            risky_expr_ratio=0.0,
        ),
    )
    assert "JOIN LATERAL" in lateral_sql.upper()
    assert re.search(r"LATERAL \(SELECT .*t0\.", lateral_sql)


def test_mysql_8022_排序表达式_上下文函数和字面量扩展能被强制覆盖() -> None:
    tables = _base_tables()

    order_generator = SQLGenerator(random_seed=818, max_sql_length=8000)
    for _ in range(160):
        order_generator.generate(
            tables,
            GenerationOptions(
                require_feature="order_expression_extensions",
                invalid_sql_ratio=0.0,
                null_compare_ratio=0.0,
                risky_expr_ratio=0.0,
            ),
        )
    for name in ["ORDER BY FIELD", "ORDER BY RAND", "ORDER BY POSITION"]:
        assert name in order_generator.coverage_counts

    context_generator = SQLGenerator(random_seed=819, max_sql_length=8000)
    context_sql = context_generator.generate(tables, GenerationOptions(require_feature="context_functions"))
    context_upper = context_sql.upper()
    for token in ["USER()", "CURRENT_USER()", "DATABASE()", "VERSION()", "CONNECTION_ID()"]:
        assert token in context_upper
    for name in ["USER", "CURRENT_USER", "DATABASE", "VERSION", "CONNECTION_ID"]:
        assert name in context_generator.coverage_counts

    literal_generator = SQLGenerator(random_seed=820, max_sql_length=8000)
    literal_sql = literal_generator.generate(tables, GenerationOptions(require_feature="literal_extensions"))
    literal_upper = literal_sql.upper()
    assert "X'0F'" in literal_upper
    assert "0XFF" in literal_upper
    assert "B'1010'" in literal_upper
    assert "HEX_LITERAL" in literal_generator.coverage_counts
    assert "BIT_LITERAL" in literal_generator.coverage_counts


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
        "index_hints",
        "optimizer_hints",
        "row_constructor",
        "quantified_subqueries",
        "correlated_subquery",
        "lateral_derived_table",
        "order_expression_extensions",
        "context_functions",
        "literal_extensions",
        "rand_expressions",
        "json_function_extensions",
        "string_function_extensions",
        "datetime_function_extensions",
        "math_function_extensions",
        "aggregate_window_extensions",
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
        "USE INDEX",
        "OPTIMIZER HINT",
        "ROW CONSTRUCTOR",
        "ANY SUBQUERY",
        "CORRELATED SUBQUERY",
        "ORDER BY FIELD",
        "USER",
        "HEX_LITERAL",
        "RAND",
        "JSON_TYPE",
        "JSON_VALID",
        "JSON_SET",
        "CHAR_LENGTH",
        "REGEXP_REPLACE",
        "DATE_SUB",
        "DATEDIFF",
        "LOG",
        "POW",
        "STDDEV_POP",
        "CUME_DIST",
    ]:
        assert name in generator.coverage_counts


def test_mysql_8022_rand和函数扩展能被强制覆盖() -> None:
    tables = _base_tables()
    feature_expectations = {
        "rand_expressions": ["RAND", "ORDER BY RAND"],
        "json_function_extensions": [
            "JSON_TYPE",
            "JSON_VALID",
            "JSON_UNQUOTE",
            "JSON_QUOTE",
            "JSON_SET",
            "JSON_REMOVE",
            "JSON_REPLACE",
            "JSON_CONTAINS_PATH",
        ],
        "string_function_extensions": [
            "CHAR_LENGTH",
            "LEFT",
            "RIGHT",
            "TRIM",
            "LTRIM",
            "RTRIM",
            "REPLACE",
            "REVERSE",
            "LOCATE",
            "INSTR",
            "REGEXP_LIKE",
            "REGEXP_REPLACE",
            "REGEXP_SUBSTR",
        ],
        "datetime_function_extensions": [
            "DATE_SUB",
            "DATEDIFF",
            "EXTRACT",
            "HOUR",
            "MINUTE",
            "SECOND",
            "TIME_TO_SEC",
            "SEC_TO_TIME",
            "TO_DAYS",
            "TO_SECONDS",
        ],
        "math_function_extensions": ["LOG", "LOG2", "LOG10", "POW", "SQRT", "SIGN", "TRUNCATE", "SIN", "COS", "TAN"],
        "aggregate_window_extensions": [
            "STDDEV_POP",
            "STDDEV_SAMP",
            "VAR_POP",
            "VAR_SAMP",
            "VARIANCE",
            "JSON_OBJECTAGG",
            "CUME_DIST",
            "PERCENT_RANK",
            "NTH_VALUE",
        ],
    }

    for feature, expected_hits in feature_expectations.items():
        generator = SQLGenerator(random_seed=900, max_sql_length=8000)
        for _ in range(320):
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
            assert "MATCH " not in upper
            assert " AGAINST" not in upper
            assert re.search(r"\bST_[A-Z_]+\s*\(", upper) is None
            assert " AS RLIKE" not in upper
        for name in expected_hits:
            assert name in generator.coverage_counts, f"{feature} 未覆盖 {name}"


def test_生成_sql_只引用已知表并包含_cte_join() -> None:
    generator = SQLGenerator(random_seed=7, max_sql_length=3000)

    sql = generator.generate(
        _tables(),
        GenerationOptions(require_cte=True, require_join=True),
    )

    assert "WITH" in sql
    assert "JOIN" in sql
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


def test_完整基表生成_sql_只引用已知表列且不生成向量函数() -> None:
    tables = _base_tables()
    known_identifiers = {table.name for table in tables}
    known_identifiers.update(column.name for table in tables for column in table.columns.values())
    known_identifiers.update(index.name for table in tables for index in table.indexes.values())
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
        assert "VEC_DISTANCE_" not in upper
        assert "VEC_FROMTEXT(" not in upper
        assert "VEC_TOTEXT(" not in upper
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


def test_generation_options_不再暴露向量强制开关() -> None:
    generator = SQLGenerator(random_seed=31, max_sql_length=8000)

    assert not hasattr(GenerationOptions(), "require_vector")
    sql = generator.generate(_base_tables())
    upper = sql.upper()

    assert "VEC_" not in upper
    assert "VECTOR" not in upper
